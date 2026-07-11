"""Known-presence composer: pure LOCAL aggregation of the consent-first identity
registry (wavr.identity_store) + the already-fused house-level "casa" network
signal + already-collected device metadata (wavr.device_meta) into an honest
house-scope "likely home" summary.

NO new scanning, NO cloud, NO re-fusion, NO I/O: this module only reads what the
caller already has (the fused `casa` RoomState, the registry's live maps, and
DeviceMeta.all()) and does arithmetic on it -- mirrors wavr.presence_report /
wavr.house_status's shape (pure, injected inputs, safe to call on every GET).

Two-level consent (see wavr.identity_store's module docstring):
  * a device being IN the registry (source='network') is consent #1 -- it is the
    ONLY thing that drives `present`/`likely_home` here. Opting out of `details`
    (consent #2) never drops a device's presence vote.
  * `detailed_addrs` (IdentityStore.detailed_net_addresses()) is consent #2 -- the
    ONLY gate for the richer `details` block. A device not in that allowlist
    always reports `details: None`, regardless of whether it is present.

House-scope, never room-scope: a network scan localizes a MAC to the whole house,
not a room (the same honesty NetworkSource/Identity already encode) -- so this
composer's `scope` is always "house" and `confidence_label` is always "coarse"
(network sits at DEFAULT_WEIGHTS["network"]=0.5 in wavr.fusion, the lowest-trust
modality); nothing here may ever claim a tighter label or a higher confidence
than the fused `casa` state itself already reports.

Honest "seen": `present` requires BOTH (a) the fused `casa` network source itself
currently reading present (the house-level ARP-sweep evidence), AND (b) that
specific MAC's own last_seen (from wavr.device_meta, populated by the scan
pipeline independently of any identity/label flag) being fresh. A registered but
not-recently-seen device reads `present: False` -- never a fabricated sighting.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.companion_presence import mac_prefix
from wavr.presence_report import DEFAULT_ACTIVE_WINDOW_S, _age_seconds, _parse

CONFIDENCE_LABEL = "coarse"  # network is house-level & coarse -- fixed, never "alta"


def _as_dict(state) -> dict | None:
    """Accept a RoomState object, its already-`.to_dict()`-ed form, or None --
    returns a plain dict (or None) either way so the rest of this module doesn't
    care which shape the caller passed."""
    if state is None:
        return None
    if isinstance(state, dict):
        return state
    to_dict = getattr(state, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def compose_known_presence(
    *,
    casa_state,
    net_registry: dict[str, str],
    detailed_addrs: set[str],
    meta_rows: dict[str, dict],
    now: datetime | None = None,
    active_window_s: float = DEFAULT_ACTIVE_WINDOW_S,
) -> dict:
    """Build the house-level known-presence summary.

    `casa_state` -- the fused RoomState for room "casa" (object or dict-shaped),
    or None when fusion has no data for it yet.
    `net_registry` -- IdentityStore.as_net_map(): {mac: person}, consent #1.
    `detailed_addrs` -- IdentityStore.detailed_net_addresses(): consent #2 allowlist.
    `meta_rows` -- DeviceMeta.all(): {mac: {first_seen, last_seen, device_type}}.
    `now` -- injected clock for deterministic tests; defaults to wall-clock UTC.
    """
    now = now or datetime.now(timezone.utc)
    casa = _as_dict(casa_state)

    # House-level network-modality evidence for THIS cycle: did the fused `casa`
    # room's own network source read present? (Never re-derived/re-fused here --
    # `casa` is trusted as-is, this is a read, not a second opinion.)
    net_source_present = False
    confidence = 0.0
    if casa is not None:
        confidence = float(casa.get("confidence", 0.0) or 0.0)
        for s in casa.get("sources", []) or []:
            if s.get("modality") == "network":
                net_source_present = bool(s.get("presence"))
                break

    corroborators: list[dict] = []
    for mac in sorted(net_registry):
        person = net_registry[mac]
        row = meta_rows.get(mac) or {}
        last_dt = _parse(row.get("last_seen"))
        age = _age_seconds(now, last_dt) if last_dt is not None else None
        fresh = age is not None and age <= active_window_s
        present = net_source_present and fresh

        entry: dict = {
            "person": person,
            "mac_prefix": mac_prefix(mac),
            "present": present,
        }
        if mac in detailed_addrs:
            entry["details"] = {
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
                "device_type": row.get("device_type"),
                "quiet_for_seconds": round(age, 1) if age is not None else None,
            }
        else:
            entry["details"] = None
        corroborators.append(entry)

    likely_home = any(c["present"] for c in corroborators)

    return {
        "scope": "house",
        "modality": "network",
        "likely_home": likely_home,
        "confidence": confidence,
        "confidence_label": CONFIDENCE_LABEL,
        "corroborators": corroborators,
    }
