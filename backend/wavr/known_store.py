"""Persisted RUNTIME "known device" allowlist -- the fix for the core
complaint that intrusion (rogue-device) alerts show many ordinary house
devices that simply have no registration, not intruders.

Today the ONLY thing that suppresses a rogue_device alert is the STATIC
WAVR_NET_MACS env allowlist (wavr.config.net_known_macs, read once at
startup -- see wavr.netinventory_service's module docstring). Naming a
device (wavr.device_meta.set_name) or pinning its type
(wavr.device_meta.set_type) does NOT mark it known -- device_meta is a
completely separate concern (display metadata + the recog type-pin signal),
never consulted for the rogue check.

This module is the RUNTIME complement: a small per-MAC known/unknown flag,
set by the local admin at any time (not just at startup), surviving
restarts. Mirrors wavr.device_meta's shape (a small sqlite store, injectable
path, ":memory:" for tests) but stores a single boolean per MAC instead of
name/type/timestamps.

The static env allowlist and this runtime store are UNIONED at scan time via
a "known_provider" callable
(wavr.netinventory_service.NetworkInventoryService.known_provider /
wavr.rules.RulesEngine.known_provider) -- never baked into a single static
set -- so a runtime mark-known takes effect on the very NEXT scan with no
restart. Marking a device known/unknown is a WRITE (state-changing), so the
HTTP route that calls set_known() (wavr.api_inventory's
POST /api/inventory/known) is gated by the same require_local CSRF guard as
every other state-changing route (wired in app.py, same rule as
device_meta's name/type routes) -- only a local admin can mark a device
known."""
from __future__ import annotations

import sqlite3

from wavr.device_meta import normalize_mac

_SCHEMA = """
CREATE TABLE IF NOT EXISTS known_devices (
    mac    TEXT PRIMARY KEY,
    known  INTEGER NOT NULL
);
"""


class KnownStore:
    """Persisted per-MAC runtime known-flag.

    `set_known(mac, known)` upserts: True marks a MAC known -- any rogue
    alert for it is dropped from the live alert log immediately (see
    wavr.netinventory_service.NetworkInventoryService.apply_known_change) and
    future scans stop reporting it rogue. False explicitly UN-marks a
    previously-known MAC ("re-arm") -- if that MAC resurfaces as unknown on a
    later scan it alerts again, exactly like a brand-new unknown device
    would. Raises ValueError on a malformed MAC (same rule as device_meta),
    letting the API route turn that into a 400 rather than reaching sqlite
    with a garbage key.
    """

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + synchronous=NORMAL (matches storage.py/occupancy_log.py): the
        # default rollback journal fsyncs on every commit, which -- when the bulk
        # "Trust all N" route commits once per device -- was a fsync storm that
        # froze the caller for seconds at airport scale. WAL amortizes that; the
        # real fix is the ONE-commit `set_known_many` below.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def set_known(self, mac: str, known: bool) -> dict:
        mac = normalize_mac(mac)
        self._conn.execute(
            """INSERT INTO known_devices (mac, known) VALUES (?, ?)
               ON CONFLICT(mac) DO UPDATE SET known = excluded.known""",
            (mac, 1 if known else 0),
        )
        self._conn.commit()
        return {"mac": mac, "known": bool(known)}

    def set_known_many(self, macs, known: bool = True) -> int:
        """Bulk upsert of many MACs to the same known state -- ONE executemany +
        ONE commit (one fsync) for the whole set, instead of the commit-per-MAC
        the single `set_known` does. This is the fix for the "Trust all N
        devices" bulk route (wavr.api_inventory): at airport scale it was
        thousands of separate commits, each a full fsync, freezing the event
        loop for seconds and hammering the disk. Malformed MACs are SKIPPED (not
        raised) so one bad entry can't abort a whole bulk trust. Returns the
        number of rows actually written."""
        rows = []
        for m in macs:
            try:
                rows.append((normalize_mac(m), 1 if known else 0))
            except ValueError:
                continue
        if not rows:
            return 0
        self._conn.executemany(
            """INSERT INTO known_devices (mac, known) VALUES (?, ?)
               ON CONFLICT(mac) DO UPDATE SET known = excluded.known""",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def is_known(self, mac: str) -> bool:
        mac = normalize_mac(mac)
        r = self._conn.execute(
            "SELECT known FROM known_devices WHERE mac = ?", (mac,)
        ).fetchone()
        return bool(r["known"]) if r else False

    def known_macs(self) -> set[str]:
        """Every MAC currently marked known=True -- the shape the
        known_provider hook (NetworkInventoryService/RulesEngine) expects.
        A MAC explicitly marked known=False is NOT included here (it is
        inert, same as never having been marked at all -- only the static
        env allowlist or a future mark-known call would make it known)."""
        rows = self._conn.execute(
            "SELECT mac FROM known_devices WHERE known = 1"
        ).fetchall()
        return {r["mac"] for r in rows}

    def close(self) -> None:
        self._conn.close()
