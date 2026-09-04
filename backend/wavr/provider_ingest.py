"""An inbound contract any external positioning system can be adapted onto.

## Why this exists instead of four vendor adapters

Cisco Spaces, Cognitive Systems, Origin Wireless and the rest each have a
location API, and every one of them is behind a partner agreement. Writing an
adapter against a schema nobody here has seen would be fabrication: the code
would look complete, compile, pass its own invented fixtures, and fail on first
contact with the real thing.

So Wavr defines the shape it accepts, and a thin adapter — written by whoever
actually holds the credentials — translates one vendor into it. That adapter is
twenty lines. This is the part that has to be right.

## One half of the declaration is enforced. The other cannot be, and says so.

A provider must be REGISTERED before it may post anything, and the registration
carries the two things a household needs: how far the provider reaches, and the
finest answer it can honestly give.

**The precision ceiling is enforced on every observation.** **Reach is not, and
no amount of code here could enforce it** — `reach` says where the VENDOR'S
copy of the data goes (`local`, `lan`, `internet`, `cloud`), which is a fact
about the other end of the integration. Wavr can publish it so a household knows
what it agreed to, and it can refuse a provider that declines to state one. It
cannot verify it from an inbound POST, and a docstring here claimed for a while
that it did.

The ceiling, on every observation:

    declared `room`, posts a count  -> the count is DROPPED and the response
                                       says so, by name, with the reason

Dropping-and-reporting rather than rejecting the batch, because a vendor adapter
sending one extra field should still deliver its presence — and rather than
dropping silently, because an integrator needs to find out from Wavr and not
from a support ticket six months later.

## Two refusals worth stating

**An unknown room is refused**, with the known ones listed. A typo would
otherwise conjure a room that appears on the dashboard, holds occupancy, and
exists nowhere in the house.

**An unregistered provider is refused.** Not "accepted with defaults" — a
default reach is the privacy failure `providers.describe` already refuses, and
a default ceiling would let anything claim `position`.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from wavr.events import SensingEvent
from wavr.fusion import COUNTING_MODALITIES, RESOLUTION_SCOPE, _SCOPE_RANK
from wavr.providers import (
    CONFIDENCE_SEMANTICS, CONF_NONE, KINDS, KIND_SPATIAL, REACH_ORDER,
    ProviderDescriptor, describe,
)

# What an external provider's evidence is fused AS. `node` is the most
# conservative presence modality Wavr has: room scope, no counting, absence
# distrusted. A provider whose declaration earns more gets it through the
# ceiling, never through the modality.
DEFAULT_MODALITY = "node"

# Modalities an external provider may declare. Deliberately short: each one has
# absence semantics and a counting rule already decided in `fusion`, and letting
# a provider name an arbitrary modality would let it inherit trust that was
# reasoned about for a different technology.
ALLOWED_MODALITIES: frozenset[str] = frozenset({"node", "pir", "ble", "wifi_csi"})

# Namespacing, so a reliability profile for an external sensor can never be
# confused with a camera of the same name.
SENSOR_PREFIX = "ext:"

MAX_BATCH = 200
MAX_ID = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_providers (
    provider_id  TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    reach        TEXT NOT NULL,
    -- Which paired device may post AS this provider. Empty means nobody has
    -- been bound, and the route then accepts loopback only -- see the module
    -- docstring on why `presence:write` alone was not enough.
    device_id    TEXT NOT NULL DEFAULT '',
    modality     TEXT NOT NULL DEFAULT 'node',
    ceiling      TEXT NOT NULL DEFAULT 'room',
    confidence   TEXT NOT NULL DEFAULT 'none',
    enabled      INTEGER NOT NULL DEFAULT 1,
    notes        TEXT NOT NULL DEFAULT '',
    created_ts   TEXT NOT NULL
);
"""


class IngestError(ValueError):
    """An observation Wavr will not accept as given."""


@dataclass(frozen=True)
class ExternalProvider:
    provider_id: str
    label: str
    kind: str
    reach: str
    modality: str = DEFAULT_MODALITY
    ceiling: str = "room"
    confidence: str = CONF_NONE
    enabled: bool = True
    notes: str = ""
    # The one paired device permitted to post as this provider. Empty means the
    # provider is loopback-only.
    device_id: str = ""

    @property
    def may_count(self) -> bool:
        """Whether this provider's counts are worth anything.

        BOTH conditions, because they answer different questions: the declared
        ceiling is what the operator agreed this technology can do, and
        `COUNTING_MODALITIES` is what fusion will actually let the modality
        assert. A provider that declared `count` while fusing as `node` would
        have its counts silently discarded downstream, which is worse than being
        told here.
        """
        return (_SCOPE_RANK.get(self.ceiling, 0) >= _SCOPE_RANK["count"]
                and self.modality in COUNTING_MODALITIES)

    def descriptor(self) -> ProviderDescriptor:
        return describe(
            self.provider_id, self.label, self.kind, self.reach,
            precision_ceiling=self.ceiling,
            confidence_semantics=self.confidence,
            observes=("presence",) + (("count",) if self.may_count else ()),
            notes=self.notes or ("An external positioning system, adapted onto "
                                 "Wavr's inbound contract."))

    def to_dict(self) -> dict:
        return {"provider_id": self.provider_id, "label": self.label,
                "kind": self.kind, "reach": self.reach,
                "modality": self.modality, "ceiling": self.ceiling,
                "confidence": self.confidence, "enabled": self.enabled,
                "may_count": self.may_count, "notes": self.notes,
                # The device id itself, not a token. Published so an operator
                # can see which adapter is allowed to speak for this provider.
                "device_id": self.device_id,
                "loopback_only": not self.device_id}


class ExternalProviderStore:
    """Which external systems may post evidence, and what each may claim."""

    def __init__(self, path: str = "wavr.db", now_fn=None):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def register(self, provider_id: str, label: str, *, kind: str = KIND_SPATIAL,
                 reach: str = "", modality: str = DEFAULT_MODALITY,
                 ceiling: str = "room", confidence: str = CONF_NONE,
                 notes: str = "", device_id: str = "") -> ExternalProvider:
        provider_id = _slug(provider_id, "provider_id")
        label = " ".join(str(label or "").split())[:120] or provider_id
        if kind not in KINDS:
            raise IngestError(f"kind must be one of {sorted(KINDS)}")
        if reach not in REACH_ORDER:
            # No default, for the reason `providers.describe` refuses one: an
            # unstated reach resolves to the most comfortable value at exactly
            # the moment nobody is paying attention.
            raise IngestError(
                f"reach must be one of {list(REACH_ORDER)} — a provider that "
                f"does not say how far it reaches cannot be enabled safely")
        if modality not in ALLOWED_MODALITIES:
            raise IngestError(f"modality must be one of "
                              f"{sorted(ALLOWED_MODALITIES)}")
        if confidence not in CONFIDENCE_SEMANTICS:
            raise IngestError("unknown confidence semantics")
        if ceiling not in _SCOPE_RANK:
            raise IngestError(f"unknown precision ceiling {ceiling!r}")
        # Capped against what the MODALITY can support, so a declaration cannot
        # buy precision the fusion path will never grant. Same rule
        # `providers.describe` applies: a declaration may lower a ceiling, never
        # raise it.
        modality_ceiling = RESOLUTION_SCOPE.get(modality, "house")
        if _SCOPE_RANK[ceiling] > _SCOPE_RANK[modality_ceiling]:
            ceiling = modality_ceiling

        with self._lock:
            self._conn.execute(
                "INSERT INTO external_providers (provider_id, label, kind, reach,"
                " device_id, modality, ceiling, confidence, enabled, notes,"
                " created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(provider_id) DO UPDATE SET label = excluded.label,"
                " kind = excluded.kind, reach = excluded.reach,"
                " device_id = excluded.device_id,"
                " modality = excluded.modality, ceiling = excluded.ceiling,"
                " confidence = excluded.confidence, notes = excluded.notes",
                (provider_id, label, kind, reach, str(device_id or "")[:MAX_ID],
                 modality, ceiling, confidence, notes, self._now()))
            self._conn.commit()
        return ExternalProvider(provider_id, label, kind, reach, modality,
                                ceiling, confidence, True, notes,
                                str(device_id or "")[:MAX_ID])

    def get(self, provider_id: str) -> ExternalProvider | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM external_providers WHERE provider_id = ?",
                (provider_id,)).fetchone()
        return _row(row) if row is not None else None

    def list(self) -> list[ExternalProvider]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM external_providers ORDER BY provider_id").fetchall()
        return [_row(r) for r in rows]

    def set_enabled(self, provider_id: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE external_providers SET enabled = ? WHERE provider_id = ?",
                (1 if enabled else 0, provider_id))
            self._conn.commit()
        return cur.rowcount > 0

    def remove(self, provider_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM external_providers WHERE provider_id = ?",
                (provider_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(row) -> ExternalProvider:
    return ExternalProvider(
        provider_id=row["provider_id"], label=row["label"], kind=row["kind"],
        reach=row["reach"], modality=row["modality"], ceiling=row["ceiling"],
        confidence=row["confidence"], enabled=bool(row["enabled"]),
        notes=row["notes"], device_id=row["device_id"])


def _slug(value, what: str) -> str:
    s = " ".join(str(value or "").split()).lower().replace(" ", "_")
    if not s or len(s) > MAX_ID or not all(c.isalnum() or c in "_-." for c in s):
        raise IngestError(f"{what} must be 1-{MAX_ID} characters of "
                          f"letters, digits, '_', '-' or '.'")
    return s


def translate(provider: ExternalProvider, observations, *, rooms,
              at_fn) -> tuple[list[SensingEvent], list[dict]]:
    """Turn a batch of posted observations into events, and say what was dropped.

    Returns `(events, problems)`. A problem is not a failure of the batch — the
    rest is still delivered — but it IS reported, with the observation index and
    the reason, so an integrator finds out from Wavr rather than from six months
    of quietly missing counts.

    `at_fn(stated) -> iso` is the caller's clock normalisation, so an external
    provider's timestamps go through `timebase` like any other foreign clock.
    """
    known = {str(r) for r in (rooms or [])}
    events: list[SensingEvent] = []
    problems: list[dict] = []

    rows = list(observations or ())
    if len(rows) > MAX_BATCH:
        raise IngestError(f"at most {MAX_BATCH} observations per request; "
                          f"got {len(rows)}")

    for i, obs in enumerate(rows):
        if not isinstance(obs, dict):
            problems.append({"index": i, "dropped": "observation",
                             "why": "not an object"})
            continue
        room = str(obs.get("room") or "").strip()
        if room not in known:
            # Refused rather than accepted: a typo would otherwise conjure a
            # room that appears on the dashboard, holds occupancy, and exists
            # nowhere in the house.
            problems.append({
                "index": i, "dropped": "observation",
                "why": (f"no room named {room!r} in this Space. Known rooms: "
                        f"{sorted(known)}")})
            continue

        present = bool(obs.get("present"))
        count = obs.get("count")
        if count is not None:
            if not provider.may_count:
                problems.append({
                    "index": i, "dropped": "count",
                    "why": (f"{provider.provider_id} is declared at "
                            f"'{provider.ceiling}' precision and fuses as "
                            f"'{provider.modality}', so it cannot assert a "
                            f"headcount. The presence was kept.")})
                count = None
            else:
                try:
                    count = max(0, int(count))
                except (TypeError, ValueError):
                    problems.append({"index": i, "dropped": "count",
                                     "why": "count was not a whole number"})
                    count = None

        confidence = obs.get("confidence")
        try:
            confidence = 0.7 if confidence is None else float(confidence)
        except (TypeError, ValueError):
            problems.append({"index": i, "dropped": "confidence",
                             "why": "confidence was not a number; used 0.7"})
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))

        sensor = str(obs.get("sensor_id") or provider.provider_id)[:MAX_ID]
        events.append(SensingEvent(
            sensor_id=f"{SENSOR_PREFIX}{provider.provider_id}:{sensor}",
            room=room,
            modality=provider.modality,
            presence=present,
            motion=0.0, breathing_bpm=None, heart_bpm=None,
            # Zero on absence, matching every other source: a report of "I do
            # not see anybody" carries no mass into the merge.
            confidence=confidence if present else 0.0,
            ts=at_fn(obs.get("at")),
            count=count if present else None,
        ))
    return events, problems
