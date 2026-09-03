"""One pass of the Discovery Inbox feed.

Lifted out of `app.py`, where it was a closure over six stores and could
therefore only be exercised by standing up a whole application. Nothing about
this is HTTP: it renews a lease, reads what the Core already knows, and turns
the parts a human must decide on into inbox items.

## The error policy, which is the interesting part

Every step is guarded, because one unreadable store must not stop the other five
— but the guards are deliberately NOT uniform, and the difference is the design:

  * `sqlite3.Error` where a wrong result would be MISLEADING. The camera lookup
    is the example: a bare `except Exception` there would swallow a typo in the
    method name, produce an empty set, and make every camera the operator
    already configured reappear in the inbox forever. That is the exact nagging
    the inbox exists to prevent, and it would look like a feature working.
  * `Exception` only where the step reaches OFF this box — the network
    inventory, peer polling — because those genuinely fail in ways SQLite does
    not, and a failure there is expected rather than a bug.

Anything that fails is logged at debug with a traceback. Silence is what makes a
broken feed indistinguishable from a quiet house.
"""
from __future__ import annotations

import logging
import sqlite3

from wavr.discovery_feed import feed_core_topology, feed_devices, feed_pending_nodes


def run_discovery_pass(*, inbox, cores, cameras, inventory, last_seen_ips: dict,
                       nodes=None, observe_peers=None,
                       discover_unpaired=None) -> int:
    """Renew this Core's lease, then raise everything a human should decide on.

    Returns the number of items observed. Dependencies are passed rather than
    reached for, so this is testable with four fakes and no application.

    `observe_peers` and `discover_unpaired` are the two halves of multi-Core
    awareness and are kept SEPARATE on purpose: the first talks to PAIRED peers
    over the certificate-pinned channel and may change who is authoritative; the
    second reads mDNS, which anything on the segment can forge, and may only
    ever produce a card for a human. Collapsing them into one callable would
    make it easy for a future change to let an advertisement move authority.
    """
    seen = 0

    # Renew this Core's lease. Nothing else calls `heartbeat()`, so without this
    # `last_seen_ts` is written once at registration and every Core reads as
    # stale 120s later -- including the one actually running.
    try:
        me = cores.self_core()
        if me is not None:
            cores.heartbeat(me.core_id)
    except sqlite3.Error:
        logging.debug("discovery feed: heartbeat failed", exc_info=True)

    # Cameras the operator already added. NOT a bare `except Exception`: see the
    # module docstring -- an empty set here silently re-offers every configured
    # camera, forever.
    try:
        configured_cams = {c.get("mac") for c in cameras.list() if c.get("mac")}
    except sqlite3.Error:
        logging.debug("discovery feed: camera list unavailable", exc_info=True)
        configured_cams = set()

    try:
        seen += feed_devices(inbox, inventory.latest_inventory(),
                             configured_camera_macs=configured_cams,
                             last_seen_ips=last_seen_ips)
    except Exception:      # noqa: BLE001 -- reaches the network
        logging.debug("discovery feed: inventory pass failed", exc_info=True)

    # Node-initiated join requests. `list_pending()` returns ONLY rows awaiting a
    # decision, so a node already approved or disabled is never re-raised as a
    # fresh Approve/Deny card.
    if nodes is not None:
        try:
            seen += feed_pending_nodes(inbox, nodes.list_pending())
        except sqlite3.Error:
            logging.debug("discovery feed: node pass failed", exc_info=True)

    # Two halves, two trust levels -- see the docstring.
    if observe_peers is not None:
        try:
            observe_peers()
        except Exception:      # noqa: BLE001 -- reaches other machines
            logging.debug("discovery feed: peer observation failed", exc_info=True)
    if discover_unpaired is not None:
        try:
            seen += discover_unpaired()
        except Exception:      # noqa: BLE001 -- reaches the network
            logging.debug("discovery feed: core discovery failed", exc_info=True)

    try:
        seen += feed_core_topology(inbox, cores.topology())
    except Exception:      # noqa: BLE001
        logging.debug("discovery feed: topology pass failed", exc_info=True)

    # Age out pending items nobody has looked at in a fortnight. The count cap in
    # `observe()` already bounds the table, so this is tidiness rather than a
    # safety valve -- but a "pending question" about a phone that visited once in
    # July is not a pending question.
    try:
        inbox.prune()
    except sqlite3.Error:
        logging.debug("discovery feed: prune failed", exc_info=True)

    return seen
