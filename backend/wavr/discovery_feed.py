"""Translation from what Wavr already senses into things a human can decide.

`discovery_inbox.py` is the queue; this is what fills it. Kept as pure functions
over already-collected data — they take a list of devices, a topology dict, a
list of pending nodes, and write items. They never scan, never probe, never
touch the network. That matters for two reasons:

  * it keeps the "discover aggressively, activate conservatively" split honest —
    the aggressive half already happened in `netinventory_service`, and this
    layer only decides what deserves a human's attention;
  * it makes every rule testable with a literal list, no LAN required.

An inbox that nobody fills is a fake feature (SS60), and an inbox that fills with
everything is noise. So the rules here are deliberately conservative: a device
the operator already marked known is not news, a camera already added is not
news, and a device seen at a new address is only news when we knew its old one.
"""
from __future__ import annotations

from wavr.discovery_inbox import (
    KIND_CAMERA_FOUND, KIND_CORE_CONTESTED, KIND_DEVICE_MOVED, KIND_DEVICE_NEW,
    KIND_NODE_PENDING, describe_device,
)

# All three of these are wired into the Core's periodic pass
# (`app.py:_discovery_once`): devices from the network inventory, pending nodes
# from NodeStore, and the Core topology.

# Device types (from `wavr.data.deviceclass`) that are worth offering as a Wavr
# sensing source rather than merely listing. Cameras are the big one: they are
# the highest-value source a household already owns.
_CAMERA_TYPES = frozenset({"camera", "ip_camera", "nvr", "doorbell"})

# Below this, we do not raise a camera item at all. A weak guess that something
# *might* be a camera, surfaced as an actionable card, trains the operator to
# distrust the inbox.
_CAMERA_MIN_CONFIDENCE = 0.5


def _dev(device, attr, default=None):
    """Read from either a `netinventory.Device` or a plain dict, so a caller can
    pass the dataclass or the already-serialised `/api/inventory` view."""
    if isinstance(device, dict):
        return device.get(attr, default)
    return getattr(device, attr, default)


def feed_devices(inbox, devices, *, known_macs=frozenset(),
                 configured_camera_macs=frozenset(),
                 last_seen_ips=None) -> int:
    """Turn one inventory snapshot into inbox items. Returns how many were
    observed (created or refreshed).

    `last_seen_ips` is an optional {mac: ip} the caller keeps between calls; it
    is what lets us tell "a device changed address" from "a device we have never
    seen". Without it, no move is ever reported — silence rather than a guess."""
    seen = 0
    for device in devices or []:
        mac = _dev(device, "mac")
        if not mac:
            continue
        ip = _dev(device, "ip")
        vendor = _dev(device, "vendor", "") or ""
        hostname = _dev(device, "hostname") or ""
        dtype = (_dev(device, "device_type", "") or "").lower()
        conf = float(_dev(device, "type_confidence", 0.0) or 0.0)
        label = hostname or vendor or "A device"

        # -- Address change. Only for a device we already had an address for.
        if last_seen_ips is not None:
            previous = last_seen_ips.get(mac)
            if previous and ip and previous != ip:
                inbox.observe(
                    KIND_DEVICE_MOVED, mac,
                    f"{label} is now at a different address.",
                    detail={"mac": mac, "from": previous, "to": ip,
                            "vendor": vendor},
                    confidence=1.0)      # we watched it happen; this is a fact
                seen += 1
            if ip:
                last_seen_ips[mac] = ip

        # -- A camera we could actually use.
        if dtype in _CAMERA_TYPES and conf >= _CAMERA_MIN_CONFIDENCE:
            if mac not in configured_camera_macs:
                inbox.observe(
                    KIND_CAMERA_FOUND, mac,
                    describe_device(hostname or vendor, vendor, "a camera", conf),
                    detail={"mac": mac, "ip": ip, "vendor": vendor,
                            "device_type": dtype},
                    confidence=conf)
                seen += 1
            continue          # a camera is not also "an unknown device"

        # -- Something new on the network.
        if not _dev(device, "known", False) and mac not in known_macs:
            inbox.observe(
                KIND_DEVICE_NEW, mac,
                describe_device(hostname or vendor, vendor,
                                dtype.replace("_", " ") if dtype else "", conf),
                detail={"mac": mac, "ip": ip, "vendor": vendor,
                        "device_type": dtype, "hostname": hostname},
                confidence=conf)
            seen += 1
    return seen


def feed_pending_nodes(inbox, nodes) -> int:
    """Sensor nodes waiting for an operator decision.

    Fed from `NodeStore.list_pending()` -- boards that called
    `POST /api/nodes/request` and hold no credential yet. The older path (the
    operator mints a code on a trusted screen, the node redeems it) produces no
    pending row at all and never appears here: by the time such a node exists it
    was already authorised, and showing an Approve/Deny card for a decision
    already made is exactly the nagging this inbox avoids.

    Callers must pass only genuinely pending nodes; this does no filtering of its
    own. The node's `name_hint`/`sensor_hint` are CLAIMS -- rendered so the
    operator can pre-fill a form, never written to the fields fusion trusts."""
    seen = 0
    for node in nodes or []:
        node_id = _dev(node, "node_id")
        if not node_id:
            continue
        name = _dev(node, "name", "") or node_id[:8]
        inbox.observe(
            KIND_NODE_PENDING, node_id,
            f"{name} wants to join as a sensor.",
            # `*_hint` and nothing else, deliberately: a pending node has no
            # `sensor_type` yet (it is "" until an operator sets it), and
            # shipping an empty trusted-looking field alongside a populated
            # claim is how a claim gets promoted by accident. The names say
            # which is which.
            detail={"node_id": node_id,
                    "name_hint": _dev(node, "name_hint", "") or name,
                    "sensor_hint": _dev(node, "sensor_hint", "")},
            confidence=1.0)
        seen += 1
    return seen


def feed_core_topology(inbox, topology) -> int:
    """Raise the one Core condition a human must resolve: two Cores claiming to
    be authoritative at the same epoch.

    Reads THIS Core's own view. It is fed by `app.py`'s peer-observation pass,
    which polls each PAIRED peer over the pinned channel and calls
    `core_registry.observe_peer` — so a genuine two-machine disagreement does
    reach here, as a `contested` topology, once both Cores have been paired.

    What it still cannot see is a Core nobody paired with. Those are surfaced
    separately as a `peer_core` card asking the operator to connect, because an
    unauthenticated advertisement must never move authority."""
    if not topology or not topology.get("contested"):
        return 0
    ids = sorted(c.get("core_id", "") for c in topology.get("cores", [])
                 if c.get("status") == "primary")
    inbox.observe(
        KIND_CORE_CONTESTED, "topology",
        "Two Cores both think they are in charge of this Space.",
        detail={"cores": ids,
                "note": "Wavr picked one so your Space keeps working, but you "
                        "should check why this happened."},
        confidence=1.0)
    return 1
