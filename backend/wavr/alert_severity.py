"""Single source of truth for Wavr's alert-severity ladder (the /api/alerts
domain).

This is DISTINCT from wavr.health_check's connectivity ladder
(ok/minor/degraded/major/critical), which grades LAN/internet *reachability* --
a continuous health status, not a discrete security alert. Keeping the two
ladders separate (but each internally single-sourced) is deliberate: it is NOT
the same as forking one gradient into three, it is two well-scoped domains.

Five tiers, Wavr's OWN wording (inventory feature #1 -- coined here, never a proprietary tool's or
a proprietary scanner's alert copy, per the standing license rule). Ordered low -> high so a
benign guest-phone sighting and a confirmed spoofed-gateway event never render
with equal prominence:

  info      benign / expected churn -- e.g. a randomized (locally-administered)
            MAC joining: the classic "a guest's phone hopped on the Wi-Fi".
  note      a new unknown device with a real (globally-unique) MAC -- worth a
            glance, not an intrusion.
  watch     a security-relevant-but-still-ambiguous LAN event (reserved for the
            transient->sustained unknown-device promotion, inventory feature #3).
  alert     a serious LAN event: an extra / rogue DHCP server, or the default
            gateway's MAC identity changing (ARP-spoof / rogue-router-adjacent).
  critical  a SUSTAINED gateway-identity change -- confirmed still present when
            the re-alert throttle window expires. The scariest, highest-
            confidence event; reserved for confirmed-and-persisting, NEVER a
            first sighting (honesty: a source may never overstate its own
            confidence).

ONE ladder for every alert kind (rogue_device / rogue_dhcp / gateway_identity)
so a consumer (the frontend banner/badge, ntfy fan-out) maps severity ->
prominence exactly ONCE, never a per-kind gradient fork.
"""
from __future__ import annotations

SEVERITY_INFO = "info"
SEVERITY_NOTE = "note"
SEVERITY_WATCH = "watch"
SEVERITY_ALERT = "alert"
SEVERITY_CRITICAL = "critical"

# Low -> high; list index == rank. Consumers order by this, never by the string
# itself (so a UI never has to hardcode the ordering of the five names).
SEVERITY_LADDER = (
    SEVERITY_INFO,
    SEVERITY_NOTE,
    SEVERITY_WATCH,
    SEVERITY_ALERT,
    SEVERITY_CRITICAL,
)


def severity_rank(severity: str) -> int:
    """Numeric rank (higher == more severe) for ordering / threshold checks.
    An unknown or None severity sorts below every real tier (rank -1) rather
    than raising -- a malformed value must never crash an alert render."""
    try:
        return SEVERITY_LADDER.index(severity)
    except ValueError:
        return -1
