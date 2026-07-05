"""F3 camera IP-drift detection + rebind suggestions.

A Tapo/RTSP camera dies SILENTLY when DHCP moves it to a new IP -- the stored
rtsp_url still points at the old address. This module turns that into an
actionable, user-confirmed rebind:

  * `suggest_rebind(camera, inventory)` -- a PURE function. Given a stored camera
    dict (with an optional `mac`) and the current LAN inventory, it returns a
    drift suggestion ONLY when the camera's stored MAC is present in the
    inventory AT A DIFFERENT IP than the one in its rtsp_url; otherwise None. It
    NEVER guesses (no MAC / MAC absent / IP unchanged / hostname URL => None).

  * `CameraHealthMonitor` -- the edge-triggered sink `wavr.sources.camera`'s
    health hook calls. On a "down" report it cross-references the stored camera +
    latest inventory and folds any drift suggestion into a small bounded ring;
    `suggestions()` reads them; `clear(name)` drops one after a rebind. Every
    store/inventory access is wrapped -- a failure is logged, never raised, so a
    bad scan can never break the source loop.

SECURITY NOTES (load-bearing):
  * A rebind is NEVER automatic -- a MAC-spoofing LAN attacker can manufacture a
    drift suggestion by impersonating a down camera's MAC, so the suggestion is
    advisory ONLY and the user must confirm (POST /api/cameras/{name}/rebind).
    The suggestion carries the inventory `vendor` + a `ts` so the user can judge.
  * The suggestion exposes only IP + MAC + vendor -- all already visible via
    /api/inventory, so it is non-sensitive. The rtsp_url (which carries creds) is
    NEVER included and NEVER logged.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone

from wavr.camera_url import rtsp_host
from wavr.netinventory import Device, _same_ip

_LOG = logging.getLogger(__name__)

# Bound the in-memory suggestion ring. Suggestions are keyed by camera name (at most
# one per camera), so this is already naturally small; the hard cap is defence against
# an unbounded roster (many rebinds / churn) growing memory without limit.
_MAX_SUGGESTIONS = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_ip_literal(host: str | None) -> bool:
    """True only for a bare IP-address literal (v4 or v6). A DNS hostname returns
    False: hostname rtsp URLs are intentionally OUT of scope for drift (DNS already
    re-resolves them), so we never suggest rebinding one."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host.strip())
        return True
    except ValueError:
        return False


def _find_by_mac(inventory: list[Device], mac: str) -> Device | None:
    for d in inventory or ():
        if (d.mac or "").strip().lower() == mac:
            return d
    return None


def suggest_rebind(camera: dict, inventory: list[Device]) -> dict | None:
    """Return a drift suggestion for `camera` or None. A suggestion is produced ONLY
    when: the camera has a stored `mac`, its current rtsp host is an IP literal, that
    MAC is present in `inventory` with a known IP, and that inventory IP DIFFERS from
    the stored host. Pure/offline -- no I/O, never raises, never touches the rtsp_url
    beyond reading its host. The result exposes IP+MAC+vendor only (non-sensitive)."""
    try:
        mac = (camera.get("mac") or "").strip().lower()
        if not mac:
            return None                       # no MAC captured -> can't detect drift
        current_host = rtsp_host(camera.get("rtsp_url") or "")
        if not _is_ip_literal(current_host):
            return None                       # hostname URL -> out of scope for drift
        dev = _find_by_mac(inventory, mac)
        if dev is None or not dev.ip:
            return None                       # camera's MAC not on the LAN right now
        if _same_ip(current_host, dev.ip):
            return None                       # still at the stored IP -> no drift
        return {
            "camera": camera.get("name"),
            "mac": mac,
            "current_ip": current_host,
            "suggested_ip": dev.ip,
            # Inventory vendor + a freshness timestamp so the user can judge the
            # suggestion (a spoofed MAC could manufacture one). `ts` is the moment we
            # confirmed this MAC holds suggested_ip in the latest inventory scan --
            # i.e. its last-seen-at-this-address time.
            "vendor": dev.vendor,
            "ts": _now(),
        }
    except Exception:
        _LOG.warning("suggest_rebind failed for a camera", exc_info=True)
        return None


class CameraHealthMonitor:
    """Edge-triggered sink for CameraSource's health hook + the drift-suggestion
    store. `get_camera(name) -> dict|None` and `latest_inventory() -> list[Device]`
    are injected (the CameraStore + NetworkInventoryService in wavr.app; canned in
    tests). Always available (like DeviceMeta) -- inert until a camera reports down
    AND a stored MAC drifts, so it costs nothing out of the box."""

    def __init__(self, get_camera, latest_inventory,
                 max_suggestions: int = _MAX_SUGGESTIONS):
        self._get_camera = get_camera
        self._latest_inventory = latest_inventory
        self._max = max_suggestions
        self._down: set[str] = set()
        self._suggestions: dict[str, dict] = {}

    def report(self, name: str, healthy: bool) -> None:
        """The health sink CameraSource calls (name, healthy). On recovery, drop the
        down-state + any stale suggestion. On a down report, cross-reference the
        stored camera against the latest inventory and fold in any drift suggestion.
        Tolerant: a store/inventory error is logged, never raised."""
        try:
            if healthy:
                self._down.discard(name)
                self._suggestions.pop(name, None)
                return
            self._down.add(name)
            cam = self._get_camera(name)
            if not cam:
                return
            inv = self._latest_inventory() or []
            sug = suggest_rebind(cam, inv)
            if sug is not None:
                self._suggestions[name] = sug
                self._trim()
        except Exception:
            _LOG.warning("camera health monitor report failed", exc_info=True)

    def suggestions(self) -> list[dict]:
        """All current drift suggestions (newest-inserted last)."""
        return list(self._suggestions.values())

    def clear(self, name: str) -> None:
        """Drop a camera's suggestion + down-state -- called after a rebind."""
        self._down.discard(name)
        self._suggestions.pop(name, None)

    def _trim(self) -> None:
        # dict preserves insertion order -> evict oldest first when over the cap.
        while len(self._suggestions) > self._max:
            self._suggestions.pop(next(iter(self._suggestions)), None)
