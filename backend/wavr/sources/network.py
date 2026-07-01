from __future__ import annotations

import re

# Matches a MAC with either "-" (Windows arp) or ":" (Unix) separators.
_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


def parse_arp_table(arp_output: str) -> set[str]:
    """Extract every MAC from raw `arp -a` output, normalized to lowercase
    colon form. Separator-agnostic (Windows uses '-', Unix ':')."""
    macs = set()
    for m in _MAC_RE.findall(arp_output):
        macs.add(m.replace("-", ":").lower())
    return macs
