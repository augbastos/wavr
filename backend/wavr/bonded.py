"""Read THIS PC's BONDED Bluetooth devices -- a SUGGESTION source for the
consent-first registry, never an auto-writer.

A device bonded (paired) to this machine is a deliberate affirmative act by its
owner, which is why it is offered PRE-CHECKED in the admin's "these are mine"
confirm step. It is still only a suggestion: the admin explicitly confirms (and
can uncheck any device a housemate paired once) before anything is written to
identity_store. This module only ENUMERATES; it never writes the registry.

Cross-OS seam, Windows-first:
  * Windows -- PowerShell `Get-PnpDevice -Class Bluetooth`, filtering InstanceId
    for the `DEV_<12 hex>` remote-device MAC (the adapter/enumerator/service rows
    have no DEV_ segment and are skipped).
  * Linux   -- `bluetoothctl devices Paired` ("Device <MAC> <name>").
  * macOS   -- stub (returns []); `system_profiler SPBluetoothDataType` is the
    future path.

SECURITY: the enumeration command is a FIXED constant -- it takes NO user input,
so there is no shell-injection surface. Every failure mode (no adapter, tool
absent, non-zero exit, unparseable output) degrades to [] rather than raising, so
a GET /api/identity/bonded is always safe. The MAC is normalized + validated and
junk rows are dropped; the name is defensively trimmed (it is rendered via
textContent on the frontend anyway).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys

from wavr.device_meta import normalize_mac

# Fixed enumeration commands -- NO interpolation, NO user input (injection-safe).
_WINDOWS_CMD = (
    "powershell", "-NoProfile", "-NonInteractive", "-Command",
    "Get-PnpDevice -Class Bluetooth | "
    "Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress",
)
_LINUX_CMD = ("bluetoothctl", "devices", "Paired")

# Remote-device MAC embedded in a Windows Bluetooth InstanceId, e.g.
# BTHENUM\...\7&...&DEV_AABBCCDDEEFF or BTHLE\DEV_AABBCCDDEEFF&...
_DEV_RE = re.compile(r"DEV_([0-9A-Fa-f]{12})", re.IGNORECASE)
# "Device AA:BB:CC:DD:EE:FF My Phone" (bluetoothctl).
_BTCTL_RE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s*(.*)$")
_MAX_NAME = 64


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def _mac_from_dev(hex12: str) -> str:
    """AABBCCDDEEFF -> aa:bb:cc:dd:ee:ff (normalized/validated)."""
    colon = ":".join(hex12[i:i + 2] for i in range(0, 12, 2))
    return normalize_mac(colon)


def _clean_name(name: str) -> str:
    n = re.sub(r"[\x00-\x1f\x7f]", "", (name or "")).strip()
    return n[:_MAX_NAME]


def parse_windows(raw: str) -> list[dict]:
    """Parse `ConvertTo-Json` output of FriendlyName/InstanceId into
    [{address, name}] -- only rows carrying a DEV_<12hex> remote-device MAC.
    Defensive: any parse error yields []; a junk MAC drops that row only."""
    try:
        data = json.loads(raw) if raw.strip() else []
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        m = _DEV_RE.search(str(item.get("InstanceId", "")))
        if not m:
            continue
        try:
            addr = _mac_from_dev(m.group(1))
        except ValueError:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        out.append({"address": addr, "name": _clean_name(str(item.get("FriendlyName", "")))})
    return out


def parse_linux(raw: str) -> list[dict]:
    """Parse `bluetoothctl devices Paired` lines into [{address, name}]."""
    out: list[dict] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        m = _BTCTL_RE.match(line.strip())
        if not m:
            continue
        try:
            addr = normalize_mac(m.group(1))
        except ValueError:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        out.append({"address": addr, "name": _clean_name(m.group(2))})
    return out


async def read_bonded(run=None) -> list[dict]:
    """Enumerate this PC's bonded Bluetooth devices as [{address, name}]. `run` is
    the subprocess seam (injectable for tests). NEVER raises: any failure -> []."""
    runner = run or _run
    try:
        if os.name == "nt":
            return parse_windows(await runner(*_WINDOWS_CMD))
        if sys.platform == "darwin":
            return []   # macOS stub (system_profiler path is future work)
        return parse_linux(await runner(*_LINUX_CMD))
    except Exception:
        logging.warning("bonded-device read failed", exc_info=True)
        return []
