from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

DEFAULT_MAP = {
    "rooms": [
        {"name": "sala",    "x": 0.0, "y": 0.0, "w": 4.0, "h": 3.0},
        {"name": "quarto",  "x": 4.2, "y": 0.0, "w": 3.5, "h": 3.0},
        {"name": "quintal", "x": 0.0, "y": 3.2, "w": 7.7, "h": 2.5},
    ]
}


def load_house_map(path: str) -> dict:
    """User's floor plan from JSON; DEFAULT_MAP on any problem (never raises)."""
    if not path:
        return DEFAULT_MAP
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("rooms"), list):
            return m
        log.warning("house map %s malformed (no rooms list); using default", path)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("house map %s unreadable (%s); using default", path, exc)
    return DEFAULT_MAP
