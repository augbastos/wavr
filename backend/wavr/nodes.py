"""Sensor NODES: the fast turnkey "flash a box -> it feeds Wavr" surface
(design 2026-07-11). A *node* is a small headless sensor the operator OWNS and
flashes (first target: ESP32 + HLK-LD2450 24GHz mmWave) that reports presence to
this Wavr instance over the LAN. It is NOT a companion phone (`DeviceStore`) and
NOT a peer Wavr instance (`PeerStore`): a node has no dashboard, no role, and can
only ever PUSH sensor readings for ONE operator-assigned room/modality.

Three separable pieces live here, each mirroring a proven pattern already in the
codebase so a node subsystem crash cannot touch device/peer auth (failure
isolation):

  * `NodeEnroller`  -- in-memory, one-time, TTL'd, per-IP rate-limited enrollment
    codes. Same defensive shape as `pairing.PairingManager`, but on redeem it
    creates a NODE row (never a Device). The pending code carries the
    operator-declared {name, sensor_type, room, transport} minted on a TRUSTED
    loopback screen -- so a node NEVER chooses its own room/modality/trust
    (anti-spoof: those are the load-bearing fields and must not be self-reported).

  * `NodeStore`     -- SQLite (shares `wavr.db`, own `nodes` table, same pattern
    as `PeerStore`/`DeviceStore`). Holds the per-node bearer-token HASH (never the
    plaintext), the pinned cert fingerprint (TOFU), the resolved presence modality
    + transport confidence cap, the kill-switch `state`, and two monotonic
    counters (`last_seq` telemetry anti-replay, `press_count` physical-reactivation
    anti-replay).

  * `node_event()`  -- pure translation from a node's telemetry payload to a
    canonical `SensingEvent`, keyed on the TRUSTED node record (room + modality +
    cap come from the row, NEVER from the frame). Reuses the already-tested
    `parse_ld2450_frame` server-side so the LD2450 wire parser has exactly ONE
    implementation.

KILL-SWITCH INVARIANT (taxonomy, non-negotiable): remote-OFF-only. `disable()` is
reachable by the loopback admin (remote can DISABLE). The disabled -> active edge
lives ONLY in `reactivate()`, which requires a NODE-INITIATED call carrying a
strictly-increasing physical `press_count` -- there is deliberately NO remote
`enable()` method on this store or in the API. `revoke()` is terminal (token
killed); no `press_count` can resurrect a revoked node.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent, Target
from wavr.sources.mmwave import parse_ld2450_frame

# -- Sensor archetype -> fusion modality ------------------------------------
# A node declares WHAT it is (operator-chosen at code-mint), and that maps to the
# presence modality it fuses as -- so like-sensors carry like-weights. An LD2450
# node is `mmwave` (weight 0.9, count-capable) exactly like the wired serial
# source. Non-presence sensors (a thermometer) map to "" -> never a fusion event.
SENSOR_MODALITY: dict[str, str] = {
    "ld2450": "mmwave",         # HLK-LD2450 24GHz position radar (first target)
    "mmwave": "mmwave",         # any other mmWave presence radar
    "pir": "pir",               # passive-IR motion (presence-only, coarser)
    "ble_beacon": "ble",        # a fixed BLE beacon anchor
    "generic": "node",          # unknown/other presence sensor -> honest coarse
    "environmental": "",        # temp/humidity/CO2 -> telemetry only, NOT presence
}

# Only radar-class sensors can honestly COUNT discrete people (mirrors
# fusion.COUNTING_MODALITIES). A PIR/BLE node must never assert a number.
COUNTING_SENSORS: frozenset[str] = frozenset({"ld2450", "mmwave"})

# Transport-trust CAP on a node's emitted confidence. A node whose firmware WE
# flashed and that presents a per-node bearer token over pinned TLS is `native`
# (full trust). An interop node reached over a shared MQTT broker (any
# broker-authorized client can publish -- weaker anti-spoof) is capped. The cap
# lives here, in the source layer, so fusion's per-modality WEIGHTS stay clean:
# transport risk scales the evidence, not the modality's trust.
TRANSPORT_CAP: dict[str, float] = {"native": 1.0, "mqtt": 0.7}

# Kill-switch states.
STATE_ACTIVE = "active"       # enrolled + enabled: telemetry accepted -> fusion
STATE_DISABLED = "disabled"   # remote-OFF: telemetry REJECTED, node told to sleep
STATE_REVOKED = "revoked"     # terminal: token dead, must re-flash + re-enroll

# Enrollment-code defenses (mirror pairing.PairingManager).
CODE_TTL_SECONDS = 300        # a headless node may take a minute to join Wi-Fi
MAX_FAILED_ATTEMPTS = 10
ATTEMPT_WINDOW_SECONDS = 60

# Reactivate abuse brake (appsec finding #3, MEDIUM). NOT a security boundary --
# the server can never verify a PHYSICAL button press, only that the caller holds
# this node's own bearer token (enforced by the API layer's _auth_node) and that
# press_count is a fresh high-water mark. This just caps how many times a given
# node_id can call reactivate() in a window, so a compromised/buggy node spamming
# the route can't hammer the store. `revoke()` is the operator's real terminal
# recourse for a node they no longer trust -- no press_count can undo it.
REACTIVATE_MAX_ATTEMPTS = 20
REACTIVATE_WINDOW_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


class NodeReactivateRateLimited(Exception):
    """Raised by `NodeStore.reactivate()` when a node_id has called reactivate more
    than REACTIVATE_MAX_ATTEMPTS times within REACTIVATE_WINDOW_SECONDS. The API
    layer (api_nodes.py) turns this into a 429. See REACTIVATE_MAX_ATTEMPTS's
    comment: this is an abuse brake, not a substitute for physical-presence proof
    (which the server cannot verify)."""


def _hash_token(token: str) -> str:
    """sha256 hex of a node's bearer token. Tokens are 256-bit random secrets, so
    a plain fast hash is right -- there is nothing to brute-force, and the
    plaintext never touches disk (same rationale as DeviceStore)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id          TEXT PRIMARY KEY,
    name             TEXT    NOT NULL,
    sensor_type      TEXT    NOT NULL,
    modality         TEXT    NOT NULL,
    room             TEXT    NOT NULL,
    transport        TEXT    NOT NULL,
    token_hash       TEXT,
    cert_fingerprint TEXT    NOT NULL DEFAULT '',
    confidence_cap   REAL    NOT NULL DEFAULT 1.0,
    state            TEXT    NOT NULL DEFAULT 'active',
    press_count      INTEGER NOT NULL DEFAULT 0,
    last_seq         INTEGER NOT NULL DEFAULT 0,
    last_seen_ts     TEXT,
    created_ts       TEXT    NOT NULL
);
"""


@dataclass(frozen=True)
class Node:
    """An enrolled node, minus its (hashed, never-returned) bearer token."""

    node_id: str
    name: str
    sensor_type: str
    modality: str
    room: str
    transport: str
    cert_fingerprint: str
    confidence_cap: float
    state: str
    press_count: int
    last_seq: int
    last_seen_ts: str | None
    created_ts: str

    def to_dict(self) -> dict:
        # Never includes token material. Safe for the loopback Nodes panel.
        return {
            "node_id": self.node_id, "name": self.name,
            "sensor_type": self.sensor_type, "modality": self.modality,
            "room": self.room, "transport": self.transport,
            "cert_fingerprint": self.cert_fingerprint,
            "confidence_cap": self.confidence_cap, "state": self.state,
            "last_seen_ts": self.last_seen_ts, "created_ts": self.created_ts,
        }


class NodeStore:
    """SQLite-backed node registry. Shares the db file with Storage/DeviceStore but
    owns its own `nodes` table -- same pattern as every other *_store.py here.
    Tokens are stored hashed; `get_by_token` is the only way a presented node token
    is checked."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # In-memory-only abuse brake for reactivate() (see NodeReactivateRateLimited
        # and REACTIVATE_MAX_ATTEMPTS above). Bounded growth: one bucket per
        # node_id that has EVER called reactivate, and a node_id here always
        # belongs to a token the caller already authenticated as (never
        # attacker-chosen), so this cannot be inflated by spoofed keys the way a
        # per-source-IP map could be.
        self._reactivate_attempts: dict[str, list[datetime]] = {}

    # -- enrollment (called by NodeEnroller.redeem) ------------------------
    def add(self, name: str, sensor_type: str, room: str,
            transport: str = "native", cert_fingerprint: str = "") -> tuple[str, str]:
        """Create a node and return (node_id, token). The token is generated here,
        stored hashed, and returned exactly once. `modality` and `confidence_cap`
        are DERIVED from the operator-declared sensor_type/transport -- never taken
        from the node -- which is the whole anti-spoof point."""
        modality = SENSOR_MODALITY.get(sensor_type, "node")
        cap = TRANSPORT_CAP.get(transport, 0.7)
        node_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes (node_id, name, sensor_type, modality, room,"
                " transport, token_hash, cert_fingerprint, confidence_cap, state,"
                " press_count, last_seq, last_seen_ts, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, ?)",
                (node_id, name, sensor_type, modality, room, transport,
                 _hash_token(token), cert_fingerprint, cap, STATE_ACTIVE, ts),
            )
            self._conn.commit()
        return node_id, token

    # -- node-token auth ---------------------------------------------------
    def get_by_token(self, token: str) -> Node | None:
        """Resolve the node presenting `token` (updating last_seen), or None if the
        token is unknown or the node is REVOKED. A DISABLED node still resolves --
        it must authenticate to receive its `sleep` heartbeat and to reactivate."""
        if not token:
            return None
        th = _hash_token(token)
        ts = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE token_hash = ?", (th,)
            ).fetchone()
            if row is None or row["state"] == STATE_REVOKED:
                return None
            self._conn.execute(
                "UPDATE nodes SET last_seen_ts = ? WHERE node_id = ?",
                (ts, row["node_id"]),
            )
            self._conn.commit()
        return self._to_node(row, last_seen=ts)

    # -- telemetry anti-replay --------------------------------------------
    def record_seq(self, node_id: str, seq: int) -> bool:
        """Accept a telemetry frame's monotonic `seq` iff it is strictly greater
        than the last one seen for this node (rejects a captured-and-replayed
        frame). Returns True + advances the counter on accept, False on replay."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None or seq <= row["last_seq"]:
                return False
            self._conn.execute(
                "UPDATE nodes SET last_seq = ? WHERE node_id = ?", (seq, node_id)
            )
            self._conn.commit()
            return True

    # -- kill-switch state machine ----------------------------------------
    def disable(self, node_id: str) -> bool:
        """Remote-OFF (allowed): active -> disabled. Idempotent; never touches a
        revoked node. Returns True if a live node moved/stayed disabled."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET state = ? WHERE node_id = ? AND state != ?",
                (STATE_DISABLED, node_id, STATE_REVOKED),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def reactivate(self, node_id: str, press_count: int) -> str | None:
        """The ONLY disabled -> active edge. NODE-INITIATED and gated on a strictly
        increasing physical `press_count` (a physical button press bumps it), so a
        replayed reactivation cannot re-enable a node the operator disabled, and
        there is no remote `enable()` at all. A revoked node stays revoked. Returns
        the node's NEW state, or None if unknown. Raises NodeReactivateRateLimited
        if this node_id has exceeded REACTIVATE_MAX_ATTEMPTS in the trailing
        REACTIVATE_WINDOW_SECONDS (an abuse brake, see that exception's docstring).

        Crisp server-enforceable contract (residual trust boundary, appsec finding
        #3): the SERVER never offers remote-enable -- structurally, no code path in
        this store or in api_nodes.py can flip a node active except THIS method,
        and this method only ever moves disabled -> active. Reaching it requires
        (a) the caller already authenticated as THIS node (its own bearer token,
        enforced by api_nodes._auth_node before this is called) and (b) a
        press_count strictly above the stored high-water mark. What the server
        CANNOT verify is that the press_count increment came from an actual finger
        on an actual button -- a fully compromised node's firmware could lie about
        that. That is the residual trust boundary, and it is bounded: a lying node
        can only re-enable ITSELF (never another node, never skip disable, never
        resurrect a revoked node). The operator's recourse for a node whose
        firmware/credential they no longer trust is `revoke()` -- terminal, and no
        press_count can undo it."""
        now = _utcnow()
        with self._lock:
            stamps = [t for t in self._reactivate_attempts.get(node_id, ())
                      if now - t < timedelta(seconds=REACTIVATE_WINDOW_SECONDS)]
            if len(stamps) >= REACTIVATE_MAX_ATTEMPTS:
                self._reactivate_attempts[node_id] = stamps
                raise NodeReactivateRateLimited(node_id)
            stamps.append(now)
            self._reactivate_attempts[node_id] = stamps

            row = self._conn.execute(
                "SELECT state, press_count FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                return None
            state = row["state"]
            if press_count > row["press_count"]:
                # Advance the high-water mark regardless of state (so a later replay
                # of a lower count is inert), and flip ONLY a disabled node on.
                new_state = STATE_ACTIVE if state == STATE_DISABLED else state
                self._conn.execute(
                    "UPDATE nodes SET press_count = ?, state = ? WHERE node_id = ?",
                    (press_count, new_state, node_id),
                )
                self._conn.commit()
                return new_state
            return state

    def revoke(self, node_id: str) -> bool:
        """Terminal: state -> revoked AND clear the token hash (belt-and-suspenders:
        get_by_token already rejects revoked, but a cleared hash can't be matched by
        any future code path). Idempotent. The node must be re-flashed + re-enrolled
        to return."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET state = ?, token_hash = NULL WHERE node_id = ?",
                (STATE_REVOKED, node_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- reads -------------------------------------------------------------
    def get(self, node_id: str) -> Node | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return self._to_node(row) if row else None

    def list(self) -> list[Node]:
        """All nodes (including revoked) for the Nodes panel. Never token material."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes ORDER BY created_ts, node_id"
            ).fetchall()
        return [self._to_node(r) for r in rows]

    @staticmethod
    def _to_node(r: sqlite3.Row, last_seen: str | None = None) -> Node:
        return Node(
            node_id=r["node_id"], name=r["name"], sensor_type=r["sensor_type"],
            modality=r["modality"], room=r["room"], transport=r["transport"],
            cert_fingerprint=r["cert_fingerprint"],
            confidence_cap=float(r["confidence_cap"]), state=r["state"],
            press_count=int(r["press_count"]), last_seq=int(r["last_seq"]),
            last_seen_ts=last_seen if last_seen is not None else r["last_seen_ts"],
            created_ts=r["created_ts"],
        )

    def close(self) -> None:
        self._conn.close()


@dataclass
class _PendingNode:
    name: str
    sensor_type: str
    room: str
    transport: str
    expires_at: datetime


class NodeEnroller:
    """In-memory registry of live enrollment codes, bound to a `NodeStore`. Nothing
    here is persisted (ephemeral by design, exactly like PairingManager). The code
    is minted on a TRUSTED loopback screen carrying the operator's declaration of
    what/where the node is; the headless node redeems it once over the LAN to
    receive its per-node bearer token."""

    def __init__(self, store: NodeStore, now_fn=_utcnow,
                 code_ttl: float = CODE_TTL_SECONDS,
                 max_failed: int = MAX_FAILED_ATTEMPTS,
                 attempt_window: float = ATTEMPT_WINDOW_SECONDS):
        self._store = store
        self._now = now_fn
        self._code_ttl = code_ttl
        self._max_failed = max_failed
        self._attempt_window = attempt_window
        self._codes: dict[str, _PendingNode] = {}
        self._failed: dict[str, list[datetime]] = {}   # source_ip -> failed stamps

    def mint_code(self, name: str, sensor_type: str, room: str,
                  transport: str = "native") -> str:
        """Mint a one-time enrollment code carrying the operator-declared node
        identity. `sensor_type` is validated against the known map so an unknown
        type can't smuggle in an unweighted modality; `transport` must be known so
        the confidence cap is always deterministic."""
        if sensor_type not in SENSOR_MODALITY:
            raise ValueError(f"unknown sensor_type: {sensor_type!r}")
        if transport not in TRANSPORT_CAP:
            raise ValueError(f"unknown transport: {transport!r}")
        if not room.strip():
            raise ValueError("room is required")
        self._purge_expired()
        code = self._fresh_code()
        self._codes[code] = _PendingNode(
            name.strip() or sensor_type, sensor_type, room.strip(), transport,
            self._now() + timedelta(seconds=self._code_ttl))
        return code

    def redeem(self, code: str, cert_fingerprint: str = "",
               source_ip: str | None = None) -> tuple[str, str] | None:
        """Redeem a code for a new node: (node_id, token) once, or None if the code
        is unknown/used/expired. One-time (consumed on first attempt). Failed
        attempts are rate-limited PER SOURCE IP so one host's junk guesses can't
        lock out a legitimate enrollment from another host. The node self-reports
        only its `cert_fingerprint` (pinned TOFU) -- never its room/modality/trust."""
        now = self._now()
        self._purge_failed(now)
        key = source_ip or ""
        if len(self._failed.get(key, ())) >= self._max_failed:
            return None
        pending = self._codes.pop(code, None)
        if pending is None or now >= pending.expires_at:
            self._failed.setdefault(key, []).append(now)
            return None
        return self._store.add(pending.name, pending.sensor_type, pending.room,
                               pending.transport, cert_fingerprint)

    def _fresh_code(self) -> str:
        for _ in range(10):
            code = f"{secrets.randbelow(100_000_000):08d}"
            if code not in self._codes:
                return code
        return f"{secrets.randbelow(100_000_000):08d}"

    def _purge_expired(self) -> None:
        now = self._now()
        self._codes = {k: v for k, v in self._codes.items() if now < v.expires_at}

    def _purge_failed(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._attempt_window)
        self._failed = {ip: keep for ip, stamps in self._failed.items()
                        if (keep := [t for t in stamps if t >= cutoff])}


def _num(v) -> bool:
    """A genuine finite number, never a bool (bool is an int subclass), and never
    NaN/+-Infinity: a node's telemetry is the first place in this codebase where an
    untrusted network client hands raw JSON floats straight into a Target/
    SensingEvent, and Python's json module accepts the non-standard NaN/Infinity
    tokens on decode -- letting one through would poison position/velocity/
    confidence with a value that breaks strict JSON re-encoding downstream
    (the frontend's JSON.parse rejects NaN/Infinity outright)."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def node_event(node: Node, payload: dict, now_iso: str | None = None) -> SensingEvent | None:
    """Translate a node's telemetry `payload` into a canonical `SensingEvent`, or
    None when the node must contribute NOTHING to fusion:
      * node.state != active  -> None (kill-switch: a disabled node's data is dropped)
      * node.modality == ""    -> None (non-presence sensor, e.g. a thermometer)

    ROOM and MODALITY are taken from the TRUSTED node record, never the payload, so
    a compromised/rogue node cannot relocate itself or masquerade as a higher-trust
    modality. Confidence is clamped to [0,1] then to the node's transport cap.

    LD2450/mmWave nodes forward RAW 30-byte report frames as hex in
    `ld2450_frames`; those are parsed HERE with the already-tested
    `parse_ld2450_frame`, so the LD2450 wire parser has one implementation, not a
    firmware copy that can drift. Other sensor types send decoded
    presence/motion/targets.

    MALFORMED TELEMETRY (appsec finding #2, MEDIUM): a node is an untrusted network
    client, and `payload` is raw attacker-shaped JSON -- a wrong-shaped field
    (`ld2450_frames`/`targets` not a list, a target's `id` not int-coercible, ...)
    used to raise an unhandled TypeError/ValueError straight into the request
    handler (a DoS vector: one bad frame could crash the telemetry route). Any such
    shape error is now caught and the WHOLE payload is dropped as a clean no-op
    (returns None, same as a non-presence sensor) rather than raised -- consistent
    with the existing per-frame `continue` below, just widened to the rest of the
    translation."""
    if node.state != STATE_ACTIVE or not node.modality:
        return None
    ts = now_iso or _utcnow_iso()

    try:
        targets: list[Target] = []
        # Cap attacker-controlled array lengths before materializing Targets: an LD2450
        # emits <=3 targets/frame, so a node batching thousands of frames or targets is
        # definitionally malformed. Truncating keeps the drop-don't-crash posture while
        # bounding memory/CPU (an uncapped 50k-target payload measured ~+23MB). A non-list
        # value still raises TypeError on the slice -> caught below -> dropped.
        _MAX_ARRAY = 64
        if node.sensor_type in ("ld2450", "mmwave") and payload.get("ld2450_frames"):
            for hexframe in payload["ld2450_frames"][:_MAX_ARRAY]:
                try:
                    raw = bytes.fromhex(hexframe)
                except (ValueError, TypeError):
                    continue                       # a malformed frame is dropped, not fatal
                targets.extend(parse_ld2450_frame(raw))
        else:
            for i, t in enumerate((payload.get("targets") or [])[:_MAX_ARRAY]):
                if not isinstance(t, dict):
                    continue
                x, y = t.get("x"), t.get("y")
                has_pos = _num(x) and _num(y)
                posture = t.get("posture") if isinstance(t.get("posture"), str) else None
                if not has_pos and posture is None:
                    continue
                targets.append(Target(
                    id=int(t.get("id", i + 1)),
                    x=float(x) if has_pos else None,
                    y=float(y) if has_pos else None,
                    velocity=float(t["velocity"]) if _num(t.get("velocity")) else None,
                    posture=posture,
                    confidence=float(t["confidence"]) if _num(t.get("confidence")) else 0.5,
                ))

        if targets:
            presence = True
            motion = max((t.velocity or 0.0 for t in targets), default=0.0)
            raw_conf = 0.9
        else:
            presence = bool(payload.get("presence"))
            motion = float(payload["motion"]) if _num(payload.get("motion")) else 0.0
            raw_conf = (float(payload["confidence"]) if _num(payload.get("confidence"))
                        else 0.7) if presence else 0.0
    except (TypeError, ValueError, OverflowError):
        # Malformed telemetry shape (e.g. `ld2450_frames`/`targets` not iterable, or
        # a target's `id` not coercible to int) -- drop, don't crash the handler.
        # OverflowError covers a huge-magnitude JSON int (x/y/motion/velocity/
        # confidence, or a target `id`) two ways: `math.isfinite()` in `_num()`
        # raises OverflowError when it can't widen a giant int to a C double, and
        # `int(t.get("id", ...))` raises OverflowError on a JSON float that decoded
        # to +-inf (e.g. the non-standard literal `1e400`) -- same "attacker-shaped
        # JSON" class as the TypeError/ValueError case above, same drop-not-crash.
        return None

    confidence = min(_clamp01(raw_conf), node.confidence_cap)
    # Only radar-class sensors may set a person COUNT (mirrors fusion's rule); a
    # PIR/BLE node never asserts a number even if the payload tries to.
    count = len(targets) if node.sensor_type in COUNTING_SENSORS else None

    return SensingEvent(
        room=node.room, modality=node.modality, presence=presence, motion=motion,
        breathing_bpm=None, heart_bpm=None, confidence=confidence, ts=ts,
        targets=tuple(targets), count=count,
    )
