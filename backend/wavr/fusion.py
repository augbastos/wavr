from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from wavr.events import SensingEvent
from wavr.spatial_frames import transform_target
from wavr.roomstate import RoomState

# Default trust weights per modality. Camera (video) is most precise; network
# (device presence) is house-level and coarse. Tunable via config later.
# `ble` (Bluetooth presence) sits between wifi_csi and network: room-ish, coarser
# than CSI but tighter than house-wide ARP.
DEFAULT_WEIGHTS = {"camera": 1.0, "mmwave": 0.9, "wifi_csi": 0.85, "ble": 0.7,
                   "network": 0.5, "sim": 0.6,
                   # Sensor nodes (design 2026-07-11): a node's declared sensor_type maps
                   # to one of these two NEW modalities (wavr.nodes.SENSOR_MODALITY) when
                   # it isn't an existing one (ld2450/mmwave nodes fuse as "mmwave" above,
                   # unchanged). `pir` (passive-IR, presence-only, coarser than mmwave) and
                   # `node` (unknown/generic presence sensor, most conservative weight).
                   "pir": 0.6, "node": 0.5}

# Modalities that can honestly COUNT people (not just detect presence). Camera runs
# person-detection (Detection.count); mmwave resolves discrete targets (len(targets)).
# Presence-only sources (network/ble/wifi_csi/sim) are excluded -- they know "someone
# is here", never "how many", so they must never set a count. ONE place the
# counting-capable set is defined; keep in sync with the sources that set count.
COUNTING_MODALITIES = frozenset({"camera", "mmwave"})

# Modalities whose presence=False is TRUSTWORTHY evidence of absence, i.e. a real "I looked
# and nobody is there" rather than "I lost them". This is a PHYSICS distinction, not a
# freshness one, and it is what lets the FUSION-B latch tell a dropout from a live negative:
#   * camera SEES the room -- a person who stops moving is still visible, so a fresh camera
#     reporting empty genuinely means empty.
#   * mmwave infers presence from motion/micro-motion -- a very still person VANISHES from
#     it. A fresh mmwave presence=False is EXACTLY the dropout FUSION-B exists to bridge, so
#     it must never be read as "empty" (that would re-introduce the count flicker).
# Presence-only sources are absent by construction: they never vouch for a count, so their
# negative has no latch to release.
# HONEST COST of this gate (do not let this rot into an unstated limitation): an
# mmwave-ONLY room -- the cheap, camera-free config -- keeps the bounded phantom. If the
# radar's last count was N and a presence-only source then holds the room occupied, that N
# (and its exact targets, which the precision ladder promotes back to position/100%) stands
# for the stale_s window before it expires. There is NO fusion-layer fix: distinguishing "a
# still person the radar lost" from "an empty room" is not information the merge has. The
# only honest lever is source-side (e.g. LD2450 micro-motion/breathing keeping a still
# person present). Camera rooms are cured; mmwave-only rooms are bounded, not cured.
# NOTE a new counting modality added to COUNTING_MODALITIES defaults to absence-DISTRUST,
# i.e. it fails toward the phantom, not toward honesty. Decide its absence semantics
# deliberately; test_trusted_absence_is_a_subset_of_counting enforces the set relation.
# How far ahead of this Core's clock a source's timestamp may sit before it
# starts costing the source weight. Two machines a couple of seconds apart is
# ordinary; a minute is a clock nobody synchronised, and beyond this a future
# timestamp ages exactly like a past one (see `_fuse`).
_SKEW_TOLERANCE_S = 5.0

TRUSTED_ABSENCE_MODALITIES = frozenset({"camera"})
assert TRUSTED_ABSENCE_MODALITIES <= COUNTING_MODALITIES, \
    "TRUSTED_ABSENCE_MODALITIES must be a subset of COUNTING_MODALITIES"

# PRECISION / RESOLUTION ladder -- the SECOND axis (how DETAILED an answer the
# present+fresh evidence can honestly support), orthogonal to confidence (how SURE
# someone is present). Finest detail each modality can HONESTLY deliver:
# house < room < count; the position rung is EARNED at runtime (a calibrated
# positioned target), never granted by presence. network/sim are pinned to house --
# one antenna localizes to the HOUSE, not a room (same rule as Identity, events.py).
# The count-capable scopes here are kept == COUNTING_MODALITIES (camera/mmwave).
RESOLUTION_SCOPE = {
    "network": "house", "sim": "house",
    "ble": "room", "wifi_csi": "room", "pir": "room", "node": "room",
    "mmwave": "count", "camera": "count",
}
# Modalities that may carry per-target COORDINATES. Derived from RESOLUTION_SCOPE
# rather than written out again, so the two cannot drift: `position` is EARNED at
# runtime from `count`, so a modality that can resolve discrete targets is exactly
# the one that can honestly say where one of them is.
#
# This exists because the merge used to gate targets on freshness alone. A node
# enrolled under a presence-only modality could put `targets:[{x, y}]` in its
# payload and those coordinates reached RoomState.targets and got drawn. The
# precision ladder never promoted them to "position", so the percentage on screen
# stayed honest -- but a coordinate invented by a sensor that cannot measure
# position is a claim about where a person is, and it travelled.
#
# Fails CLOSED: a modality absent from RESOLUTION_SCOPE is not locatable. A new
# modality has to be added deliberately, which is the decision this is meant to
# force.
LOCATABLE_MODALITIES = frozenset(
    m for m, scope in RESOLUTION_SCOPE.items() if scope == "count")
assert LOCATABLE_MODALITIES == COUNTING_MODALITIES, (
    "locatable and counting have diverged: decide deliberately which modality "
    "may place a body on the map, and say so here")

_SCOPE_RANK = {"none": 0, "house": 1, "room": 2, "count": 3, "position": 4}
_RANK_SCOPE = {v: k for k, v in _SCOPE_RANK.items()}
_RANK_PCT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}
# Capability-gap key -> the next rung to strive for; None when topped out (or vacant).
_NEXT_BY_RANK = {0: None, 1: "add_room_sensor", 2: "add_counting_sensor",
                 3: "calibrate_camera_position", 4: None}
# Min per-target confidence to treat an (x, y) as a drawable exact POSITION rather
# than a room-scope blob. Byte-shared intent with the frontend POS_EST_MAX
# (index.html) -- ONE threshold, two surfaces: top rung reached (backend) equals
# crisp dot drawn (frontend).
_POSITION_QUALITY_MIN = 0.6

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
    """Explainable fusion. Per room, confidence = agreement × strength, where `strength`
    is the best present evidence (weight × the source's own confidence × freshness decay).
    This stops a lone weak source (e.g. coarse network) from ever reporting 100% — that
    claim holds, and it is `strength` that delivers it.

    `agreement` distinguishes THREE things, and the distinction is the whole point:

      NO EVIDENCE          nothing is reporting -> confidence 0
      AGREEING EVIDENCE    agreement 1.0 -> confidence is `strength`
      CONFLICTING EVIDENCE agreement < 1.0 -> confidence falls below `strength`

    Only a source that can PROVE absence creates conflict. Every first-party source
    emits confidence=0.0 with presence=False, so on the plain arithmetic an absent
    source contributes mass=0 to numerator and denominator alike and drops out of the
    ratio — which was the whole behaviour here for a long time, and `agreement` was
    identically 1.0 whenever anything was present.

    That default is right for most absences. An absent source usually means "I cannot
    see them" (a still person vanishing from radar, someone outside the camera's FOV),
    not "nobody is here", and voting a present room down on it would flicker the room
    every time somebody sat still.

    But which absences ARE evidence is a question this module already answered, for the
    count latch: `TRUSTED_ABSENCE_MODALITIES`. A fresh camera presence=False is a look at
    an empty room; a fresh mmWave presence=False is a dropout. So a fresh dissent from a
    trusted-absence modality now enters the DENOMINATOR carrying its own trust
    (weight × decay × reliability, NOT the event's confidence, which is 0.0 by
    convention) and stays out of the numerator. Two strong sources that disagree no
    longer read like two strong sources that agree, and a stale camera still asserts
    nothing.

    Each source's own reading rides in `sources[]` either way, so a disagreement is
    SURFACED to the UI, never only arithmetic.

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
                 stale_s: float | None = None, vacate_s: float | None = None,
                 reliability_fn=None, mount_fn=None):
        self._weights = weights if weights is not None else DEFAULT_WEIGHTS
        self._threshold = threshold
        # Injectable clock returning an aware UTC "now". When None (default) each
        # source is aged against the room's newest event, which keeps a live
        # stream fully fresh and stays deterministic for fixed-timestamp tests.
        # Pass now_fn=lambda: datetime.now(timezone.utc) for wall-clock aging.
        self._now_fn = now_fn
        self._freshness_s = _DEFAULT_FRESHNESS_S if freshness_s is None else freshness_s
        self._stale_s = _DEFAULT_STALE_S if stale_s is None else stale_s
        # LOW fix: fail fast on an inverted/degenerate window instead of letting it
        # silently skip the linear decay curve. `_freshness()` already has a defensive
        # `stale_s <= freshness_s` branch (avoids a division by zero / negative-slope
        # decay), but that branch's actual EFFECT is confusing: every source jumps
        # straight from full trust (1.0) to dead (0.0) the instant it crosses
        # freshness_s, with no stale/decaying middle window at all -- exactly the
        # footgun a misconfigured WAVR_SOURCE_FRESHNESS_S/WAVR_SOURCE_STALE_S env pair
        # would hit silently. Raise here, at construction, so the operator sees the
        # mistake immediately rather than a live box that stops decaying anything.
        if self._stale_s <= self._freshness_s:
            raise ValueError(
                f"FusionEngine: stale_s ({self._stale_s}) must be greater than "
                f"freshness_s ({self._freshness_s}) -- otherwise a source's trust "
                "jumps straight from full weight to dead with no decay window "
                "(see _freshness())."
            )
        # Asymmetric occupancy dwell: how long `occupied` is held after
        # confidence falls below threshold before it may flip to vacant
        # (0 disables the dwell).
        self._vacate_s = _DEFAULT_VACATE_S if vacate_s is None else vacate_s
        # `reliability_fn(sensor_id, room) -> (factor, reason)` — what THIS
        # sensor has earned in THIS room, from counted checks. None = no
        # reliability wired, every sensor at its modality constant, which is the
        # behaviour every install had before this existed and the behaviour a
        # fresh install must keep. A callable rather than a store so this module
        # imports no storage and a test can pass a lambda.
        self._reliability_fn = reliability_fn
        # `mount_fn(sensor_id, room) -> MountPose | None` — where a sensor sits
        # and which way it faces, so a target it reported in its OWN frame can be
        # placed on the floor plan. None (or no mount for that sensor) means the
        # coordinate is dropped rather than guessed: a radar reports millimetres
        # from itself, and rendering that as room-local puts the person in the
        # wrong corner and then calls it position-level precision.
        self._mount_fn = mount_fn
        # room -> (modality, sensor_id) -> event. Keyed per SENSOR, not per
        # modality: two cameras in one room are two independent voices that may
        # disagree, carry their own reliability and fail independently. A source
        # with no per-instance identity has sensor_id "", so it occupies the
        # single slot `(modality, "")` and behaves exactly as it always did.
        self._latest: dict[str, dict[tuple[str, str], SensingEvent]] = {}
        self._occupied_state: dict[str, bool] = {}     # room -> last debounced occupied
        self._vacate_since: dict[str, datetime] = {}   # room -> when a pending vacate began
        # FUSION-B: last-known person_count/targets while `occupied` is held, keyed by
        # room -> {"count": int, "targets": list[dict], "ts": datetime}. Lets a STILL
        # counting source (e.g. mmwave on someone barely moving) survive a single/multi-
        # frame presence dropout without person_count flickering N -> None -> N on every
        # fuse -- see the latch block in `_fuse`. Bounded by `self._stale_s` so a long-
        # dead counting source can never keep vouching for a headcount forever.
        self._count_latch: dict[str, dict] = {}

    def update(self, event: SensingEvent) -> RoomState:
        room_events = self._latest.setdefault(event.room, {})
        try:
            _as_utc(event.ts)
            valid_ts = True
        except (TypeError, ValueError):
            valid_ts = False

        if valid_ts:
            room_events[(event.modality, event.sensor_id)] = event
            ts = event.ts
        else:
            # A malformed/unparseable ts must never be stored: once in
            # `_latest` it would poison this sensor's slot and make every
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
        # Keyed by the SENSOR key, not the modality: two cameras decay
        # independently, and one going stale must not silently gate the other's
        # targets out of the merge.
        decays: dict[tuple[str, str], float] = {}
        # sensor key -> the event's OWN parsed ts. The count latch stamps itself with this, not
        # with `ref`: stamping with `ref` re-dated the latch on every fuse that still saw a
        # decay>0 counting event, so the stale_s bound measured from the last FUSE instead of
        # from the last real COUNT and the latch outlived its documented window (see the stamp
        # below).
        event_ts: dict[tuple[str, str], datetime] = {}
        for key, e in events.items():
            modality = e.modality
            try:
                e_ts = _as_utc(e.ts)
            except (TypeError, ValueError):
                # Defensive: a stored event with an unparseable ts must never
                # crash fusion for the room's other, healthy sources. Treat it
                # as contributing no evidence (same as a dead source).
                decays[key] = 0.0
                sources.append({"modality": modality, "sensor_id": e.sensor_id,
                                "presence": e.presence,
                                "confidence": round(e.confidence, 3),
                                "age_s": None, "health": "invalid_ts",
                                "count": None})
                continue
            # Age is the DISTANCE from now, not the time SINCE now.
            #
            # This was `max(0.0, ref - e_ts)`, which is defensive about the
            # arithmetic and wrong about the world: it turns every future
            # timestamp into age zero, and age zero is full weight. A board
            # whose clock is an hour fast therefore read as "fresh", at full
            # confidence, for the whole hour — and kept reading that way,
            # because it kept sending — while the honest camera beside it went
            # dead after ten minutes. One unsynchronised ESP32 could hold a
            # room at 0.9 off an observation that was an hour old.
            #
            # A timestamp ahead of now is a broken clock, and a broken clock is
            # as untrustworthy forward as backward. `_SKEW_TOLERANCE_S` keeps
            # the ordinary second or two between two machines free.
            atraso = (ref - e_ts).total_seconds()
            age_s = (max(0.0, -atraso - _SKEW_TOLERANCE_S) if atraso < 0
                     else atraso)
            decay, health = self._freshness(age_s)
            decays[key] = decay
            event_ts[key] = e_ts
            # Reliability SCALES the modality constant; it never replaces it.
            # A modality's weight is a statement about physics (a camera can see
            # more than a PIR); reliability is a statement about this particular
            # unit in this particular room. Bounded at 1.0 upward, so a lucky
            # sensor can never out-vote a physically better one.
            rel_factor, rel_reason = 1.0, ""
            if self._reliability_fn is not None and e.sensor_id:
                try:
                    rel_factor, rel_reason = self._reliability_fn(e.sensor_id, room)
                except Exception:      # noqa: BLE001
                    # A reliability lookup is an OPINION about a sensor. If it
                    # fails, the sensor's own reading is still evidence — falling
                    # back to the modality constant is the honest degradation,
                    # and dropping the source would be the sensor paying for a
                    # storage fault.
                    rel_factor, rel_reason = 1.0, ""
            base = self._weights.get(modality, 0.5) * decay * rel_factor
            mass = base * e.confidence
            # A DISSENT is a source that can prove absence, is still awake, and
            # says the room is empty. It carries `base` — its own trust, aged
            # and scaled — because the wire convention for an absence is
            # confidence 0.0, so multiplying by the event's confidence would
            # multiply the dissent away, which is precisely the defect: the
            # source drops out of the ratio and `agreement` stays 1.0 however
            # hard it disagrees.
            #
            # Only TRUSTED_ABSENCE_MODALITIES qualify, and the reasoning is the
            # one `counting_live_negative` already uses further down: a fresh
            # mmWave presence=False is a still person the radar lost, not an
            # empty room, and voting a present room down on it would flicker
            # every time somebody sat still. A camera's fresh negative is a
            # look at the room.
            dissente = (not e.presence
                        and decay > 0.0
                        and modality in TRUSTED_ABSENCE_MODALITIES)
            if dissente:
                mass = base
            den += mass
            if e.presence:
                num += mass
                strength = max(strength, mass)
            # `sensor_id` rides along so a consumer can tell two cameras apart --
            # the whole point of the per-sensor key. Empty for a source with no
            # per-instance identity, never invented.
            row = {"modality": modality, "sensor_id": e.sensor_id,
                   "presence": e.presence,
                   "confidence": round(e.confidence, 3),
                   "age_s": round(age_s), "health": health,
                   "count": (e.count if modality in COUNTING_MODALITIES else None)}
            # Carried so the share below can be worked out once `den` is final.
            # Not published: `mass` is an internal magnitude and only its SHARE
            # of the total means anything to a reader.
            row["_mass"] = mass
            # Published ONLY when it actually changed the arithmetic. A
            # `reliability: 1.0` on every row is noise that trains a reader to
            # stop looking; a row that carries one is a row where the answer
            # moved, and the reason says by how much and on what evidence.
            if rel_factor != 1.0:
                row["reliability"] = round(rel_factor, 3)
                row["reliability_reason"] = rel_reason
            sources.append(row)
            if e.presence and e.breathing_bpm is not None:
                vitals = {"breathing_bpm": e.breathing_bpm, "heart_bpm": e.heart_bpm}
        # Person COUNT (additive, honest) -- computed HERE, BEFORE raw_occupied/debounce,
        # so FUSION-A below can pull `occupied` True off a fresh, present counting source
        # (see that block). Only counting-capable modalities that are PRESENT and still
        # fresh (decay>0) may vouch for a number -- the SAME gate as the targets pass
        # below. Among those, the highest-weight source wins when counts disagree
        # (deterministic precedence, mirroring the targets pass-through); each source's
        # own count still rides in `sources[]` above so a disagreement is SURFACED, never
        # silently resolved. NEVER feeds num/den/strength -- confidence is provably
        # unchanged by this loop. None = no counting-capable source vouches for a number
        # here (unknown, not a fabricated 0). `live_count` is the CURRENT frame's count;
        # the FUSION-B latch below decides the RoomState's actual `person_count`.
        # -- Each source's actual share of the vote ---------------------------
        #
        # Published because the dashboard was computing it from a COPY of
        # `DEFAULT_WEIGHTS` kept in the frontend, which knows nothing about the
        # freshness decay or the per-sensor reliability factor that also went
        # into `mass`. So the evidence panel showed an authoritative-looking
        # percentage that was wrong for exactly the sensors a person is looking
        # at it about: the aging one and the measured-unreliable one.
        #
        # One producer. The share is a share of `den`, so it sums to 1 across
        # the sources that voted, and a source with no mass reads 0 rather than
        # being hidden — a sensor that contributed nothing is a fact worth
        # seeing, not an absence.
        for row in sources:
            row["share"] = round(row.pop("_mass") / den, 3) if den > 0 else 0.0

        live_count: int | None = None
        live_count_ts = None   # the winning count event's OWN ts -- what the latch stamps with
        best_cw = -1.0
        for key, e in events.items():
            modality = e.modality
            if modality not in COUNTING_MODALITIES:
                continue
            if not (e.presence and decays.get(key, 0.0) > 0.0):
                continue
            if e.count is None:
                continue
            w = self._weights.get(modality, 0.5)
            if w > best_cw:
                best_cw = w
                live_count = int(e.count)
                live_count_ts = event_ts.get(key)

        agreement = num / den if den > 0 else 0.0
        # Defensive clamp: a single out-of-range source confidence (negative or
        # >1) must never drive the fused confidence outside [0, 1].
        confidence = round(min(1.0, max(0.0, agreement * strength)), 3)
        # FUSION-A: a fresh, PRESENT counting source (camera/mmwave -- the two highest-
        # trust, most-precise modalities, see COUNTING_MODALITIES) asserting count>=1
        # pulls `occupied` True even when the blended confidence sits below threshold.
        # Rationale: `occupied=False ∧ person_count>0` is an incoherent state that also
        # BLINDS the intrusion path (room_unrecognized/house_unrecognized in watch.py
        # read person_count regardless of `occupied`) -- a missed intruder is a worse
        # failure than an extra "occupied" pill on a low-confidence detection.
        # `confidence` itself is NOT touched here, so the UI still renders the honest
        # low % (this is the same shape as the existing vacate-dwell HOLD, where
        # occupied=True with confidence<threshold is already a legal, shipped state).
        # live_count == 0 (a present source explicitly counting nobody) does NOT pull
        # occupied -- `0 > 0` is False.
        # FUSION-C: a fresh, PRESENT presence-only source (network/BLE/wifi_csi/sim/pir/node
        # -- everything that DETECTS presence but cannot COUNT, i.e. NOT in COUNTING_MODALITIES)
        # pulls `occupied` True even when the blended confidence sits below threshold. These
        # sources honestly know "someone is here" (never how many, never precisely where); the
        # privacy-default "Presence" tier (network + Bluetooth, cameras OFF) blends at most
        # weight*present_confidence = 0.5*0.8 = 0.40 for network (0.7*0.7 = 0.49 for BLE) --
        # BOTH hard-below the 0.50 occupancy threshold. So WITHOUT this pull a camera-less home
        # can NEVER read occupied: a person's own phone/laptop on the LAN would show "Empty
        # home / no presence detected" forever, which is exactly the field bug this fixes. Same
        # shape as FUSION-A and the vacate-dwell HOLD: `confidence` is NOT touched (the UI keeps
        # rendering the honest low %), only the boolean flips. Vacancy still runs the asymmetric
        # dwell below, and the release is honest -- when every presence-only source reports
        # present=False (after the source-side grace smooths a single missed scan), the pull
        # lets go and the room vacates. This is presence semantics only; `confidence`, count,
        # targets and the intrusion path are all unchanged.
        house_present = any(
            e.presence and decays.get(k, 0.0) > 0.0
            for k, e in events.items() if e.modality not in COUNTING_MODALITIES
        )
        raw_occupied = (confidence >= self._threshold
                        or (live_count is not None and live_count > 0)
                        or house_present)
        occupied, pending_s = self._debounce_occupancy(room, raw_occupied, ref)
        # English at the source, because every human-facing string in this
        # product is English at the source and the frontend's catalogue is
        # keyed BY that English.
        #
        # This composed `presente`/`vazio`/`ocupado`/`confirmando saída` —
        # Portuguese baked into the engine. It reached an English household
        # verbatim in the room "Why?" panel, as
        # `ble: vazio · network: vazio → 0% ocupado`, and it was equally
        # unreachable for a Portuguese one: a catalogue keyed on English source
        # strings has nothing to match against a Portuguese key. One decision,
        # wrong in both languages.
        parts = [f"{s['modality']}: {'present' if s['presence'] else 'empty'}"
                 for s in sources]
        explanation = " · ".join(parts) + f" → {int(confidence * 100)}% occupied"
        if pending_s is not None:
            # Confidence has dropped below threshold but the room is still HELD
            # occupied by the dwell -- show the countdown, do not hide the doubt.
            rem = math.ceil(pending_s)
            explanation += f", confirming exit {rem // 60}:{rem % 60:02d}"

        best_targets: list = []
        best_w = -1.0
        for key, e in events.items():
            # TWO gates, and neither implies the other.
            #
            # Freshness (below): a stale, dead or invalid-ts source must not pass
            # its targets through -- a decayed-to-zero source is
            # indistinguishable from an absent one.
            #
            # Authority (here): a source whose modality cannot RESOLVE a target
            # must not place one, however fresh it is and however well-formed its
            # payload. Freshness is not authority, and a payload shape that
            # accepts x/y is not permission to send it.
            if e.modality not in LOCATABLE_MODALITIES:
                continue
            if e.presence and e.targets and decays.get(key, 0.0) > 0.0:
                w = self._weights.get(e.modality, 0.5)
                if w > best_w:
                    best_w = w
                    # Into the room frame, or without a position at all. A
                    # coordinate whose frame Wavr cannot resolve is not a
                    # position — the same rule the rest of this engine applies
                    # to unknown counts and unknown capabilities.
                    mount = None
                    if self._mount_fn is not None and e.sensor_id:
                        try:
                            mount = self._mount_fn(e.sensor_id, room)
                        except Exception:      # noqa: BLE001
                            # A mount lookup failing is a configuration fault.
                            # It must not place the person anywhere; leaving
                            # `mount` None drops the coordinate, which is the
                            # honest direction.
                            mount = None
                    placed = [transform_target(t, mount=mount)[0]
                              for t in e.targets]
                    best_targets = [t.to_dict() for t in placed]

        # FUSION-B: latch person_count/targets across a single/multi-frame presence
        # dropout of a STILL counting source while `occupied` is held by the debounce
        # above. Without this, a very-still person can single-frame `present=False` on
        # mmwave and person_count flickers None -> N -> None on every fuse, which (a)
        # blinds a still-present intrusion check that reads person_count and (b) churns
        # occupancy_log with a row per flicker (person_count is an exact-match field
        # there, so any None<->N change is an insert). Bounded by `self._stale_s` -- the
        # SAME dead-source bound freshness decay already uses -- so a long-dead counting
        # source can never keep vouching for a headcount indefinitely off a
        # presence-only source holding the room occupied; that would overclaim.
        count_held = False
        if not occupied:
            # Vacant room surfaces no count -- matches the UI's own
            # `rs.occupied && person_count>0` gate, and there is nothing left to latch.
            self._count_latch.pop(room, None)
            person_count = None
        elif live_count is not None:
            # Stamp with the COUNT EVENT's own ts, never with `ref`. A counting source is
            # allowed to be up to stale_s old and still vouch (decay>0), so stamping with
            # `ref` re-dated the latch on EVERY fuse that still saw that same aging event:
            # the stale_s bound then measured from the last FUSE, not from the last real
            # COUNT, and a dead counting source + a chatty presence-only source could keep
            # vouching for a headcount for ~2x stale_s. The docstring below promises the
            # bound is stale_s — this is what makes that true.
            self._count_latch[room] = {"count": live_count, "targets": best_targets,
                                       "ts": live_count_ts or ref}
            person_count = live_count
        else:
            # FUSION-C FIX: `live_count is None` conflates TWO OPPOSITE states — a counting
            # source that went SILENT (a dropout: stale/dead/no count this frame — the only
            # thing FUSION-B exists to bridge, "we don't know") and a counting source that is
            # AWAKE and explicitly reporting presence=False (a LIVE NEGATIVE — "we DO know:
            # empty"). The loop above skips a fresh present=False counting source entirely
            # (`if not (e.presence and decay > 0): continue`), so it lands here looking exactly
            # like a dropout.
            # Before FUSION-C the difference was academic: a sustained live negative dropped
            # the blended confidence, `occupied` went False, and the `not occupied` branch
            # cleared the latch. FUSION-C holds `occupied` True off ANY present presence-only
            # source, which removed that release path — so a camera reporting EMPTY would keep
            # vouching for a stale headcount, with the stale exact targets, which the precision
            # ladder then promotes back to "position"/100%, for the whole stale_s window.
            # Phantom people on the map of a room the camera says is empty.
            # A live negative is STRONGER evidence than the stale_s timer: a source that is
            # awake and says empty releases the latch NOW. The room may still read occupied
            # (a presence-only source vouches for THAT honestly) but with person_count=None and
            # no targets — "someone is home, we don't know how many" — which is exactly what a
            # presence-only detection can honestly claim. A real dropout still latches.
            # Only TRUSTED_ABSENCE_MODALITIES qualify: a fresh mmwave presence=False is a
            # STILL PERSON vanishing from the radar (the very dropout FUSION-B bridges), not
            # an empty room — reading it as a negative would re-introduce the count flicker.
            counting_live_negative = any(
                (not e.presence) and decays.get(k, 0.0) > 0.0
                for k, e in events.items()
                if e.modality in COUNTING_MODALITIES
                and e.modality in TRUSTED_ABSENCE_MODALITIES
            )
            latch = self._count_latch.get(room)
            if counting_live_negative:
                self._count_latch.pop(room, None)
                person_count = None
            elif latch is not None and (ref - latch["ts"]).total_seconds() <= self._stale_s:
                person_count = latch["count"]
                if not best_targets:
                    best_targets = list(latch["targets"])
                count_held = True
            else:
                self._count_latch.pop(room, None)
                person_count = None
        if count_held:
            # Explanation-only surface, mirroring the pending-vacate countdown above
            # (`pending_s` is likewise never a RoomState field) -- the minimal honest
            # surface for "this number is latched, not this instant's evidence" without
            # a contract change to RoomState/its consumers.
            explanation += ", contagem mantida"

        # PRECISION / RESOLUTION ladder (additive axis, DISTINCT from confidence).
        # Runs AFTER the FUSION-B latch has finalized person_count / best_targets, as
        # a pure READ over already-fused values -- it never touches num/den/strength,
        # so confidence is provably unchanged by this block. It reuses the EXACT
        # (presence and decays>0) freshness gate the count/targets passes above use, so
        # the ladder recedes honestly the same frame a source goes off/stale (camera
        # boot-OFF is never in events; a stale source has decay==0 -> excluded).
        best_rank = 0
        if occupied:
            for key, e in events.items():
                modality = e.modality
                if e.presence and decays.get(key, 0.0) > 0.0:
                    best_rank = max(best_rank,
                                    _SCOPE_RANK[RESOLUTION_SCOPE.get(modality, "house")])
            # A count in hand (live OR honestly latched by FUSION-B) floors the rung
            # at count: person_count is the honest carrier, so a still person whose
            # counting source single-frame drops keeps the count rung via the latch.
            if person_count is not None:
                best_rank = max(best_rank, _SCOPE_RANK["count"])
            # A counting-capable source present but NOT vouching a number caps at room
            # -- never claim a headcount the evidence does not support.
            elif best_rank >= _SCOPE_RANK["count"]:
                best_rank = _SCOPE_RANK["room"]
            # position is EARNED, never granted by presence: needs >=1 positioned
            # target (x/y set) at calibrated quality (>= _POSITION_QUALITY_MIN) AND a
            # count in hand. best_targets are dicts (t.to_dict()); FUSION-B-latched
            # targets are included, so a latched still person keeps the position rung.
            if person_count is not None and any(
                    t.get("x") is not None and t.get("y") is not None
                    and (t.get("confidence") or 0.0) >= _POSITION_QUALITY_MIN
                    for t in best_targets):
                best_rank = _SCOPE_RANK["position"]
        # not occupied -> best_rank stays 0 -> none: the occupied-False-with-count>0
        # incoherent state stays impossible on this axis too.
        precision_level = _RANK_SCOPE[best_rank]
        precision_pct = _RANK_PCT[best_rank]
        precision_next = _NEXT_BY_RANK[best_rank]

        # Identity pass-through (non-biometric "who is home"). METADATA ONLY: this
        # rides the SAME present + fresh (decay>0) gate as targets and NEVER feeds
        # num/den/strength/agreement above — so `confidence` is provably unchanged
        # whether or not identities are present. Deduped by person, keeping the
        # entry with the stronger (higher/closer) rssi; a labelled entry with an
        # rssi always beats one without.
        merged: dict[str, dict] = {}
        for key, e in events.items():
            if not (e.presence and e.identities and decays.get(key, 0.0) > 0.0):
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
                         identities=identities, person_count=person_count,
                         explanation=explanation, ts=ts,
                         precision_level=precision_level, precision_pct=precision_pct,
                         precision_next=precision_next)


def house_person_count(states) -> int | None:
    """House-level person count = sum of the per-room person_count values that are
    known (not None). None when NO room has a counting-capable source vouching for a
    number -- an honest "unknown", never a fabricated 0. Accepts RoomState objects or
    their to_dict() form. Single source of truth for the house total; do not re-derive
    it elsewhere. Counts distinct rooms, so a person in one room is counted once;
    overlapping sensor coverage across rooms can still double-count -- an inherent
    limit of summing per-room counts, surfaced honestly rather than hidden."""
    total: int | None = None
    for s in states:
        c = s.get("person_count") if isinstance(s, dict) else getattr(s, "person_count", None)
        if c is None:
            continue
        total = (total or 0) + int(c)
    return total
